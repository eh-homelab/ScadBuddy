package sbfleet

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

// stream is one connection the fake change stream saw.
type stream struct {
	LastEventID, Auth, Accept string
}

// fakeStream serves the change stream: connection n gets script[n] (the last one again
// after the script ends), and then the server closes it, unless hold is set and the
// step opened a stream.
type fakeStream struct {
	mu     sync.Mutex
	seen   []stream
	script []func(w http.ResponseWriter) bool
	hold   bool
}

func (f *fakeStream) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != base+"/changes" || r.Method != "GET" {
		http.NotFound(w, r)
		return
	}
	f.mu.Lock()
	n := len(f.seen)
	f.seen = append(f.seen, stream{r.Header.Get("Last-Event-ID"), r.Header.Get("Authorization"), r.Header.Get("Accept")})
	step := f.script[min(n, len(f.script)-1)]
	f.mu.Unlock()
	if step(w) && f.hold {
		<-r.Context().Done()
	}
}

func (f *fakeStream) streams() []stream {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]stream(nil), f.seen...)
}

// frames writes an SSE 200 with the given raw frames.
func frames(raw ...string) func(w http.ResponseWriter) bool {
	return func(w http.ResponseWriter) bool {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		for _, f := range raw {
			_, _ = io.WriteString(w, f)
		}
		w.(http.Flusher).Flush()
		return true
	}
}

func status(code int) func(w http.ResponseWriter) bool {
	return func(w http.ResponseWriter) bool {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(code)
		_, _ = io.WriteString(w, `{"detail":"no"}`)
		return false
	}
}

func ready(id string, resync, reconnect bool) string {
	return fmt.Sprintf("id: %s\nevent: ready\ndata: {\"resync\":%t,\"reconnect\":%t}\n\n", id, resync, reconnect)
}

// change is a change frame as the hub writes it: the stored payload as a JSON string in "p".
func change(id, kind string, recipients ...string) string {
	ids := `[]`
	if len(recipients) > 0 {
		ids = `["` + recipients[0] + `"`
		for _, r := range recipients[1:] {
			ids += `,"` + r + `"`
		}
		ids += `]`
	}
	payload := fmt.Sprintf(`{"id":"%s","at":"2026-11-25T10:00:00Z","kind":"%s","seq":1,"recipients":%s,"topics":[]}`, id, kind, ids)
	quoted := fmt.Sprintf("%q", payload)
	return fmt.Sprintf("id: %s\nevent: change\ndata: {\"c\":\"scadbuddy_events\",\"p\":%s}\n\n", id, quoted)
}

// follow runs Subscribe until want events arrive (or the test times out), and returns them.
func follow(t *testing.T, f *fakeStream, want int) []fleetchat.Event {
	t.Helper()
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	b := newBus(srv.URL, func() (string, error) { return "tok", nil })
	b.minDelay, b.maxDelay = time.Millisecond, 4*time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var mu sync.Mutex
	var got []fleetchat.Event
	done := make(chan error, 1)
	go func() {
		done <- b.Subscribe(ctx, func(ev fleetchat.Event) {
			mu.Lock()
			got = append(got, ev)
			if len(got) == want {
				cancel()
			}
			mu.Unlock()
		})
	}()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Subscribe returned %v, want context.Canceled", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("Subscribe did not return after its context ended")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(got) < want {
		t.Fatalf("got %d events %+v, want %d", len(got), got, want)
	}
	return got
}

func TestBusDeliversRecipients(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{frames(
		": keepalive\n\n",
		ready("e0", false, false),
		change("e1", "fleet.other", sid),
		change("e2", "fleet.message", sid, other),
	)}}
	got := follow(t, f, 2)
	// A stream with no Last-Event-ID starts at the newest event: a Resync first.
	want := []fleetchat.Event{{Resync: true}, {SessionIDs: []string{sid, other}}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("events %+v, want %+v", got, want)
	}
	s := f.streams()[0]
	if s.Auth != "Bearer tok" || s.LastEventID != "" || s.Accept != "text/event-stream" {
		t.Errorf("first stream %+v", s)
	}
}

// Review Focus 5: after a pruned or unknown Last-Event-ID the stream says resync in its
// ready frame, and the Bus turns that into Event{Resync: true}; a reconnected frame too.
func TestBusResync(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{frames(
		ready("e9", true, true),
		"id: e9\nevent: reconnected\ndata: {\"resync\":false}\n\n",
	)}}
	got := follow(t, f, 2)
	want := []fleetchat.Event{{Resync: true}, {Resync: true}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("events %+v, want %+v", got, want)
	}
}

func TestBusReconnectsWithLastEventID(t *testing.T) {
	f := &fakeStream{script: []func(http.ResponseWriter) bool{
		frames(ready("e0", false, false), change("e1", "fleet.message", sid)),
		status(503),
		frames(ready("e1", false, true), change("e2", "fleet.message", other)),
	}}
	f.hold = false
	got := follow(t, f, 3)
	// The first stream had no Last-Event-ID (a Resync); the reconnect's replay lost nothing.
	want := []fleetchat.Event{{Resync: true}, {SessionIDs: []string{sid}}, {SessionIDs: []string{other}}}
	if !reflect.DeepEqual(got[:3], want) {
		t.Errorf("events %+v, want %+v", got, want)
	}
	s := f.streams()
	if len(s) < 3 {
		t.Fatalf("streams %+v", s)
	}
	if s[0].LastEventID != "" || s[1].LastEventID != "e1" || s[2].LastEventID != "e1" {
		t.Errorf("Last-Event-IDs %q %q %q, want \"\" e1 e1", s[0].LastEventID, s[1].LastEventID, s[2].LastEventID)
	}
}

// A 401 or 403 does not end Subscribe: the credential may rotate.
func TestBusRetriesRefusals(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{
		status(401), status(403),
		frames(ready("e0", false, false), change("e1", "fleet.message", sid)),
	}}
	follow(t, f, 1)
	if n := len(f.streams()); n < 3 {
		t.Errorf("%d streams, want 3", n)
	}
}

func TestBusTokenErrorRetries(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{
		frames(ready("e0", false, false), change("e1", "fleet.message", sid)),
	}}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	var mu sync.Mutex
	calls := 0
	b := newBus(srv.URL, func() (string, error) {
		mu.Lock()
		defer mu.Unlock()
		calls++
		if calls < 3 {
			return "", errors.New("no file yet")
		}
		return "tok", nil
	})
	b.minDelay, b.maxDelay = time.Millisecond, 2*time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err := b.Subscribe(ctx, func(fleetchat.Event) { cancel() })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Subscribe: %v", err)
	}
	if n := len(f.streams()); n != 1 {
		t.Errorf("%d streams, want 1 (none without a credential)", n)
	}
}

func TestBusPublishIsANoOp(t *testing.T) {
	b := NewBus("http://127.0.0.1:1", func() (string, error) { return "", errors.New("unused") })
	if err := b.Publish(context.Background(), []string{sid}); err != nil {
		t.Fatal(err)
	}
}

func TestBackoff(t *testing.T) {
	b := newBus("http://x", nil)
	var got []time.Duration
	d := time.Duration(0)
	for range 9 {
		d = b.next(d)
		got = append(got, d)
	}
	want := []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second, 4 * time.Second,
		8 * time.Second, 16 * time.Second, 30 * time.Second, 30 * time.Second, 30 * time.Second}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("delays %v, want %v", got, want)
	}
}

// A stream that goes silent (no frame, no keepalive) is dropped and followed again.
func TestBusDropsASilentStream(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{
		frames(ready("e0", false, false)),
		frames(ready("e0", false, true), change("e1", "fleet.message", sid)),
	}}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	b := newBus(srv.URL, func() (string, error) { return "tok", nil })
	b.minDelay, b.maxDelay, b.silence = time.Millisecond, 2*time.Millisecond, 50*time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var got []fleetchat.Event
	err := b.Subscribe(ctx, func(ev fleetchat.Event) {
		got = append(got, ev)
		if len(ev.SessionIDs) > 0 {
			cancel()
		}
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Subscribe: %v", err)
	}
	if s := f.streams(); len(s) != 2 || s[1].LastEventID != "e0" {
		t.Errorf("streams %+v", s)
	}
	want := []fleetchat.Event{{Resync: true}, {SessionIDs: []string{sid}}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("events %+v, want %+v", got, want)
	}
}

// A failure before the first stream: that stream still has no Last-Event-ID, starts at the
// newest event, and so says Resync for whatever was sent in the gap.
func TestBusResyncsAfterAFailedFirstConnect(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{
		status(503),
		frames(ready("e0", false, false), change("e1", "fleet.message", sid)),
	}}
	got := follow(t, f, 2)
	want := []fleetchat.Event{{Resync: true}, {SessionIDs: []string{sid}}}
	if !reflect.DeepEqual(got[:2], want) {
		t.Errorf("events %+v, want %+v", got, want)
	}
	if s := f.streams(); len(s) < 2 || s[1].LastEventID != "" {
		t.Errorf("streams %+v, want a second with no Last-Event-ID", s)
	}
}

// A ready or change frame that cannot be read may have hidden a delivery: a Resync.
func TestBusResyncsOnUnreadableFrames(t *testing.T) {
	f := &fakeStream{hold: true, script: []func(http.ResponseWriter) bool{frames(
		ready("e0", false, true),
		"event: ready\ndata: {oops\n\n",
		"id: e1\nevent: change\ndata: {\"c\":\"scadbuddy_events\",\"p\":\"not json\"}\n\n",
		change("e2", "fleet.message", sid),
	)}}
	got := follow(t, f, 3)
	want := []fleetchat.Event{{Resync: true}, {Resync: true}, {SessionIDs: []string{sid}}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("events %+v, want %+v", got, want)
	}
}
