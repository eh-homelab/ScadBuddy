package bus

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
)

// The subscribe side of the bus (#7768): what the API's one LISTEN connection
// (core/pg_listener.py) hears, fanned out to every stream this process serves. The change
// streams (agent_changes/hub.py) and the realtime socket (api/realtime.py) both read it.
//
// One LISTEN connection per process, outside the request pool: Postgres delivers a NOTIFY to
// every session that listens on its channel, so each replica hears every NOTIFY, whichever
// process sent it. The connection hands on one notification at a time, in the order Postgres
// delivered them (commit order), and each subscriber keeps that order.
//
// The listener never waits on a subscriber. It only appends to each subscriber's bounded
// queue. When the connection drops and comes back, every subscriber gets a gap: what was
// NOTIFYed meanwhile reached nobody here, and its reader replays the log or resyncs.

// Channels are the channels the hub listens on: the event bus, and the agent's channels
// (agent_changes/hub.py CHANNELS).
var Channels = []string{
	channel,
	"scadbuddy_input",
	"scadbuddy_pending",
	"scadbuddy_mcp",
	"scadbuddy_warm_evict",
	"scadbuddy_settings",
}

// Channel is the event bus's channel (core/events.py PG_CHANNEL).
const Channel = channel

const (
	// backoff and maxBackoff are core/pg_listener.py's: the wait before a new connection.
	backoff    = 500 * time.Millisecond
	maxBackoff = 30 * time.Second
)

// Notice is one NOTIFY heard: its channel and payload as sent. On the event bus's channel,
// Event is the payload decoded, nil when it does not decode.
type Notice struct {
	Channel string
	Payload string
	Event   *Event
}

// LoggedID is the logged event's id the notice carries, "" for none: another channel, a
// payload that does not decode, or a kind the log never holds (Event.NotifyOnly).
func (n *Notice) LoggedID() string {
	if n.Event == nil || n.Event.NotifyOnly() {
		return ""
	}
	return n.Event.ID
}

// Item is what a subscriber reads: a notice, a gap (changes were lost here), or the end.
type Item struct {
	Notice *Notice
	Gap    bool
	End    bool
}

// Overflow is what a subscriber's full queue does with one more notice.
type Overflow int

const (
	// GapOnOverflow drops the whole queue and leaves a gap (agent_changes/hub.py).
	GapOnOverflow Overflow = iota
	// DropOldest drops the oldest notice and counts it (core/events.py Subscription).
	DropOldest
)

// Options are a subscriber's.
type Options struct {
	// Transport names the subscriber's kind in the metrics: "changes" or "ws".
	Transport string
	// Keep says which notices the subscriber gets; nil keeps every one.
	Keep func(*Notice) bool
	// Limit is the most notices its queue holds.
	Limit    int
	Overflow Overflow
	// Budget, when set, is what the subscriber counts against (Budget).
	Budget *Budget
}

// Budget is a change stream's place in the stream caps (agent_changes/hub.py ChangeHub.open).
type Budget struct {
	// Key is the credential and peer address; Group, when not "", a budget of its own of at
	// most GroupMax streams, which counts neither by key nor against the total.
	Key      string
	Group    string
	GroupMax int
}

// ErrFull is a stream refused: the process, its credential or its group has no room.
type ErrFull struct{ Reason string }

func (e *ErrFull) Error() string { return e.Reason }

// Hub is the process's LISTEN connection and its subscribers.
type Hub struct {
	pool *pgxpool.Pool
	// PerCredential and Total are agent_changes/hub.py STREAMS_PER_CREDENTIAL and STREAMS_MAX.
	PerCredential, Total int

	start sync.Once
	stop  context.CancelFunc
	done  chan struct{}

	mu       sync.Mutex
	subs     map[*Sub]struct{}
	closed   bool
	ready    chan struct{}
	isReady  bool
	last     string
	live     string
	metrics  hubMetrics
	listened int
}

type hubMetrics struct {
	subscribers metric.Int64UpDownCounter
	frames      metric.Int64Counter
	resyncs     metric.Int64Counter
	reconnects  metric.Int64Counter
}

// NewHub is the hub over pool's database. meter, when not nil, gets its metrics. It starts
// listening at Start, or at its first subscriber.
func NewHub(pool *pgxpool.Pool, meter metric.MeterProvider) *Hub {
	if meter == nil {
		meter = noop.NewMeterProvider()
	}
	m := meter.Meter("github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus")
	h := &Hub{pool: pool, PerCredential: 4, Total: 64, subs: map[*Sub]struct{}{},
		ready: make(chan struct{}), done: make(chan struct{})}
	// An instrument's error is only for an invalid name, fixed here; a failed one is a no-op.
	h.metrics.subscribers, _ = m.Int64UpDownCounter("scadbuddy.events.subscribers",
		metric.WithDescription("Event stream subscribers open, by transport (changes, ws)."))
	h.metrics.frames, _ = m.Int64Counter("scadbuddy.events.frames",
		metric.WithDescription("Event stream frames sent, by transport and frame."))
	h.metrics.resyncs, _ = m.Int64Counter("scadbuddy.events.resyncs",
		metric.WithDescription("Event stream resyncs sent, by transport and reason."))
	h.metrics.reconnects, _ = m.Int64Counter("scadbuddy.events.listener.reconnects",
		metric.WithDescription("Times the LISTEN connection came back after a drop."))
	return h
}

// Start begins to listen, once, until Close. It waits up to wait for the first LISTEN.
func (h *Hub) Start(wait time.Duration) {
	h.start.Do(func() {
		ctx, cancel := context.WithCancel(context.Background())
		h.stop = cancel
		go func() {
			defer close(h.done)
			h.listen(ctx)
		}()
	})
	select {
	case <-h.ready:
	case <-time.After(wait):
	}
}

// Close ends every subscriber, opens no more, and stops listening.
func (h *Hub) Close() {
	h.mu.Lock()
	h.closed = true
	subs := h.list()
	h.mu.Unlock()
	for _, s := range subs {
		s.offer(Item{End: true})
	}
	h.start.Do(func() { close(h.done) })
	if h.stop != nil {
		h.stop()
	}
	<-h.done
}

func (h *Hub) list() []*Sub {
	out := make([]*Sub, 0, len(h.subs))
	for s := range h.subs {
		out = append(out, s)
	}
	return out
}

// Last is the id of the last event this process heard, "" for none
// (PgNotifyEventBus.last_event_id).
func (h *Hub) Last() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.last
}

// Live is Last, but "" after the connection came back until a fresh event is heard: a
// NOTIFY sent during the gap was never heard here (PgNotifyEventBus.live_event_id).
func (h *Hub) Live() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.live
}

// Subscribe opens a subscriber. A budgeted one past its caps is an *ErrFull.
func (h *Hub) Subscribe(o Options) (*Sub, error) {
	h.mu.Lock()
	closed := h.closed
	h.mu.Unlock()
	if !closed {
		h.Start(5 * time.Second)
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return nil, &ErrFull{"the process is shutting down"}
	}
	if b := o.Budget; b != nil {
		group, total, key := 0, 0, 0
		for s := range h.subs {
			switch sb := s.opts.Budget; {
			case sb == nil:
			case sb.Group != "":
				if sb.Group == b.Group {
					group++
				}
			default:
				total++
				if sb.Key == b.Key {
					key++
				}
			}
		}
		switch {
		case b.Group != "" && group >= b.GroupMax:
			return nil, &ErrFull{fmt.Sprintf("%d change streams are open for %s", b.GroupMax, b.Group)}
		case b.Group == "" && total >= h.Total:
			return nil, &ErrFull{fmt.Sprintf("%d change streams are open", h.Total)}
		case b.Group == "" && key >= h.PerCredential:
			return nil, &ErrFull{fmt.Sprintf("this credential has %d streams open", h.PerCredential)}
		}
	}
	s := &Sub{hub: h, opts: o, wake: make(chan struct{}, 1)}
	h.subs[s] = struct{}{}
	h.metrics.subscribers.Add(context.Background(), 1, transport(o.Transport))
	return s, nil
}

// Sent counts one frame sent.
func (h *Hub) Sent(via, frame string) {
	h.metrics.frames.Add(context.Background(), 1, metric.WithAttributes(
		attribute.String("transport", via), attribute.String("frame", frame)))
}

// Resynced counts one resync told to a client, and why: "gap" (this process lost
// changes), "overflow" (the reader fell behind), "place" (its place could not be replayed).
func (h *Hub) Resynced(via, reason string) {
	h.metrics.resyncs.Add(context.Background(), 1, metric.WithAttributes(
		attribute.String("transport", via), attribute.String("reason", reason)))
}

func transport(via string) metric.AddOption {
	return metric.WithAttributes(attribute.String("transport", via))
}

func (h *Hub) listen(ctx context.Context) {
	delay := backoff
	for ctx.Err() == nil {
		conn, err := h.connect(ctx)
		if err != nil {
			if ctx.Err() == nil {
				slog.WarnContext(ctx, "the event stream's LISTEN connection failed; trying again",
					"error", err.Error())
			}
			select {
			case <-ctx.Done():
			case <-time.After(time.Duration(float64(delay) * (0.5 + rand.Float64()/2))):
			}
			delay = min(delay*2, maxBackoff)
			continue
		}
		delay = backoff
		h.connected()
		for {
			n, err := conn.WaitForNotification(ctx)
			if err != nil {
				if ctx.Err() == nil {
					slog.WarnContext(ctx, "the event stream's LISTEN connection dropped", "error", err.Error())
				}
				break
			}
			h.heard(n.Channel, n.Payload)
		}
		closing, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = conn.Close(closing)
		cancel()
	}
}

func (h *Hub) connect(ctx context.Context) (*pgx.Conn, error) {
	config := h.pool.Config().ConnConfig.Copy()
	config.Tracer = nil // a wait of hours is no query
	config.RuntimeParams["application_name"] = "scadbuddy-core-listen"
	conn, err := pgx.ConnectConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	for _, ch := range Channels {
		if _, err := conn.Exec(ctx, "LISTEN "+pgx.Identifier{ch}.Sanitize()); err != nil {
			_ = conn.Close(context.Background())
			return nil, err
		}
	}
	return conn, nil
}

// connected marks every subscriber with a gap after a reconnect (and any that opened before
// the first connection), as hub.py _connected and pg_events.py _connected do.
func (h *Hub) connected() {
	h.mu.Lock()
	h.listened++
	again := h.listened > 1
	if again {
		h.live = ""
	}
	subs := h.list()
	if !h.isReady {
		h.isReady = true
		close(h.ready)
	}
	h.mu.Unlock()
	if again {
		slog.Warn("the event stream's LISTEN connection is back; every stream replays its gap")
		h.metrics.reconnects.Add(context.Background(), 1)
	}
	for _, s := range subs {
		s.offer(Item{Gap: true})
	}
}

// heard fans one notification out. Subscribers first, then the place: hub.py and
// pg_events.py _received do it in that order.
func (h *Hub) heard(ch, payload string) {
	n := &Notice{Channel: ch, Payload: payload}
	if ch == channel {
		if e, err := Decode(payload); err == nil {
			n.Event = e
		}
	}
	h.mu.Lock()
	subs := h.list()
	h.mu.Unlock()
	for _, s := range subs {
		s.offer(Item{Notice: n})
	}
	if n.Event != nil {
		h.mu.Lock()
		h.last, h.live = n.Event.ID, n.Event.ID
		h.mu.Unlock()
	}
}

// Publish hands a notice to every subscriber as if the connection had heard it: for tests.
func (h *Hub) Publish(ch, payload string) { h.heard(ch, payload) }

// Gap marks every subscriber with a gap as a reconnect does: for tests.
func (h *Hub) Gap() {
	h.mu.Lock()
	h.listened++
	h.mu.Unlock()
	h.connected()
}

// Sub is one subscriber's queue. The listener feeds it; its reader calls Next.
type Sub struct {
	hub  *Hub
	opts Options
	wake chan struct{}

	mu    sync.Mutex
	items []Item
	ended bool
	// Dropped counts the notices a DropOldest queue lost (core/events.py Subscription.dropped).
	dropped int
	// overflows counts the times a GapOnOverflow queue was dropped (hub.py overflows).
	overflows int
}

// Keep is the subscriber's filter: whether it carries n.
func (s *Sub) Keep(n *Notice) bool { return s.opts.Keep == nil || s.opts.Keep(n) }

// Dropped is how many notices the queue lost by falling behind.
func (s *Sub) Dropped() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dropped
}

// Overflows is how many times the queue was dropped for a gap.
func (s *Sub) Overflows() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.overflows
}

func (s *Sub) offer(it Item) {
	if it.Notice != nil && !s.Keep(it.Notice) {
		return
	}
	s.mu.Lock()
	switch {
	case s.ended:
		// Told to end: nothing after it is read.
	case it.End && s.opts.Overflow == GapOnOverflow:
		// Past the bound too: a stream told to end must end, or it holds the server's drain.
		s.items, s.ended = []Item{it}, true
	case it.End:
		s.items, s.ended = append(s.items, it), true
	case s.opts.Overflow == DropOldest && len(s.items) >= s.opts.Limit:
		s.items = append(s.items[1:], it)
		s.dropped++
	case len(s.items) >= s.opts.Limit:
		s.overflows++
		s.items = []Item{{Gap: true}}
	case it.Gap && s.opts.Overflow == GapOnOverflow && len(s.items) > 0 && s.items[len(s.items)-1].Gap:
		// One replay covers both.
	default:
		s.items = append(s.items, it)
	}
	s.mu.Unlock()
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// Next is the next item, waiting at most wait (forever for 0); ok is false when wait passed
// with nothing. The end of ctx is the end.
func (s *Sub) Next(ctx context.Context, wait time.Duration) (it Item, ok bool) {
	var timeout <-chan time.Time
	if wait > 0 {
		timer := time.NewTimer(wait)
		defer timer.Stop()
		timeout = timer.C
	}
	for {
		s.mu.Lock()
		if len(s.items) > 0 {
			it = s.items[0]
			s.items = s.items[1:]
			s.mu.Unlock()
			return it, true
		}
		s.mu.Unlock()
		select {
		case <-s.wake:
		case <-timeout:
			return Item{}, false
		case <-ctx.Done():
			return Item{End: true}, true
		}
	}
}

// Close forgets the subscriber. Closing it twice does nothing.
func (s *Sub) Close() {
	h := s.hub
	h.mu.Lock()
	_, open := h.subs[s]
	delete(h.subs, s)
	h.mu.Unlock()
	if open {
		h.metrics.subscribers.Add(context.Background(), -1, transport(s.opts.Transport))
	}
}
