// Package realtime is area ws: the browser's realtime socket WS /api/v1/ws, as
// backend/scadbuddy/api/realtime.py serves it (#7768). The page follows topics and is told
// when something under one changed; frames carry kinds and ids, never content.
//
// Client to server: {"type": "subscribe" | "unsubscribe", "topics": [...]}, {"type": "check",
// "id", "since", "topics"} and {"type": "pong"}. Server to client: "subscribed" (with "last",
// the last event this process heard, while it heard every event since), "changed" (a check's
// answer), "event", "resync", "ping" every 25 s, and "error" for a bad frame. The socket
// subscribes to the bus before it reads a frame, so a "subscribed" is sent with the bus
// already listening.
package realtime

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/trace"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// Path is the socket's route.
const Path = "/api/v1/ws"

const (
	// PingEvery is realtime.py PING_SECONDS.
	PingEvery = 25 * time.Second
	// MaxTopics, RateBurst, RatePerSecond, MaxFrameChars, CheckMaxEvents, CheckPage,
	// CheckMaxSince and CheckQueue are realtime.py's.
	MaxTopics      = 64
	RateBurst      = 40
	RatePerSecond  = 20.0
	MaxFrameChars  = 16_384
	CheckMaxEvents = 1000
	CheckPage      = 500
	CheckMaxSince  = 8
	CheckQueue     = 32
	// QueueSize is core/events.py DEFAULT_QUEUE_SIZE: a socket's bus subscription.
	QueueSize = 256
	// SignedOut is realtime.py SIGNED_OUT: the close code of a revoked session's socket.
	SignedOut = 4401
	// DefaultSockets is core/config.py DEFAULT_REALTIME_SOCKETS.
	DefaultSockets = 256
	via            = "ws"
)

// Socket serves the realtime socket and gives every other request to Next.
type Socket struct {
	Hub  *bus.Hub
	Pool *pgxpool.Pool
	Next http.Handler
	// Gate resolves the handshake's caller and applies SCADBUDDY_AUTH_MODE to it, as to the
	// public routes: the socket is an "api" route (auth/routes.py).
	Gate *auth.Gate
	// AllowedOrigins is SCADBUDDY_ALLOWED_ORIGINS, PublicURL SCADBUDDY_PUBLIC_URL (the
	// setting's value when it has no row).
	AllowedOrigins []string
	PublicURL      string
	// Devices is the devices area, nil when SCADBUDDY_CORE_AREAS does not name it: then a
	// device token's handshake is refused (device.go).
	Devices DeviceTokens
	// slots are the sockets that may be open at once (SCADBUDDY_REALTIME_SOCKETS).
	slots chan struct{}
	// gated is open behind Gate.
	gated http.Handler
}

// New is the socket over hub and pool, with its settings read from env and the settings
// table, once: realtime_sockets takes a restart in Python too. gate is the process's gate:
// its mode, and its count of what enforce would refuse.
func New(ctx context.Context, hub *bus.Hub, pool *pgxpool.Pool, next http.Handler, env func(string) string,
	gate *auth.Gate) (*Socket, error) {
	s := &Socket{Hub: hub, Pool: pool, Next: next, Gate: gate, PublicURL: env("SCADBUDDY_PUBLIC_URL")}
	s.gated = gate.Wrap(auth.Route{Class: "api", Template: Path}, http.HandlerFunc(s.open))
	for item := range strings.SplitSeq(env("SCADBUDDY_ALLOWED_ORIGINS"), ",") {
		if item = strings.TrimSpace(item); item != "" {
			s.AllowedOrigins = append(s.AllowedOrigins, item)
		}
	}
	sockets := DefaultSockets
	if raw := env("SCADBUDDY_REALTIME_SOCKETS"); raw != "" {
		n, err := strconv.Atoi(strings.TrimSpace(raw))
		if err != nil || n < 0 {
			return nil, fmt.Errorf("SCADBUDDY_REALTIME_SOCKETS must be at least 0, not %q", raw)
		}
		sockets = n
	}
	if stored, ok, err := setting(ctx, pool, "realtime_sockets"); err != nil {
		return nil, err
	} else if ok {
		var n int
		if json.Unmarshal(stored, &n) == nil && n >= 0 {
			sockets = n
		}
	}
	s.slots = make(chan struct{}, sockets)
	return s, nil
}

// setting is a stored setting's JSON value; ok is false when it has no row.
func setting(ctx context.Context, pool *pgxpool.Pool, name string) (json.RawMessage, bool, error) {
	var value json.RawMessage
	err := pool.QueryRow(ctx, `SELECT value FROM settings WHERE name = $1`, name).Scan(&value)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, false, nil
	}
	return value, err == nil, err
}

func (s *Socket) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != Path || !isUpgrade(r) {
		s.Next.ServeHTTP(w, r)
		return
	}
	if deviceBearer(r) {
		// A device's token is checked in every mode, never by the gate (device.go).
		s.openDevice(w, r)
		return
	}
	telemetry.SetRoute(r.Context(), Path)
	// In enforce the gate refuses a handshake before it is accepted (403 with no body), as
	// auth/check.py closes it (1008); in observe it counts it and serves it.
	s.gated.ServeHTTP(w, r)
}

// open serves a handshake the gate let through.
func (s *Socket) open(w http.ResponseWriter, r *http.Request) {
	c := auth.CallerOf(r.Context())
	who := caller{viewer: c.Principal}
	if c.Kind == auth.KindPerson {
		// Only a session's socket closes on its revocation (realtime.py _serve).
		who.session, who.principal = c.SessionID, c.Principal
	}
	s.accepted(w, r, who)
}

// accepted serves a handshake whose caller is who: the origin check, the socket cap, the
// upgrade, then the socket.
func (s *Socket) accepted(w http.ResponseWriter, r *http.Request, who caller) {
	if !s.originAllowed(r) {
		refuse(w)
		return
	}
	select {
	case s.slots <- struct{}{}:
	default:
		refuse(w) // realtime.py closes it with 1013 before it accepts it
		return
	}
	defer func() { <-s.slots }()
	c, err := accept(w, r)
	if err != nil {
		return
	}
	telemetry.Streaming(r.Context())
	// The hijacked connection outlives the request's context; the session ends on its own.
	s.serve(context.WithoutCancel(r.Context()), c, who)
}

// caller is a session's socket, as realtime.py _serve keeps it: session is the hex SHA-256
// of the session's token, principal its person. Any other caller's is empty. viewer is the
// person any person's socket is for (a session's or an access token's), "" when signed out.
// A device's socket has its device id and the Authorization header it opened with.
type caller struct {
	session, principal, viewer string
	device, authorization      string
}

// person is who a person's socket follows a device call for: "anonymous" when signed out,
// as the devices area's routes name that caller.
func (c caller) person() string {
	if c.viewer == "" {
		return "anonymous"
	}
	return c.viewer
}

// originAllowed is core/origins.py origin_allowed with the public URL and the allowed
// origins: no Origin (not a browser page), a loopback one, or one of those.
func (s *Socket) originAllowed(r *http.Request) bool {
	values, given := r.Header["Origin"]
	if !given || len(values) == 0 {
		return true
	}
	origin := values[0]
	norm, ok := normalisedOrigin(origin)
	if !ok {
		return s.refused(r, origin)
	}
	if host := hostOf(norm); host == "localhost" || host == "127.0.0.1" || host == "[::1]" {
		return true
	}
	public := s.PublicURL
	if stored, found, err := setting(r.Context(), s.Pool, "public_url"); err != nil {
		slog.WarnContext(r.Context(), "could not read the public URL", "error", err.Error())
	} else if found {
		public = ""
		_ = json.Unmarshal(stored, &public)
	}
	for _, candidate := range append([]string{public}, s.AllowedOrigins...) {
		if c, ok := normalisedOrigin(candidate); ok && candidate != "" && c == norm {
			return true
		}
	}
	return s.refused(r, origin)
}

func (s *Socket) refused(r *http.Request, origin string) bool {
	slog.WarnContext(r.Context(), "refused a realtime socket from an origin that is not the public URL's "+
		"and not in SCADBUDDY_ALLOWED_ORIGINS", "origin", origin)
	return false
}

// normalisedOrigin is origins.py normalised_origin: scheme://host[:port], lower-cased, the
// default port dropped; ok is false when it is not http(s).
func normalisedOrigin(value string) (string, bool) {
	u, err := url.Parse(strings.TrimSpace(value))
	if err != nil || u.Scheme != "http" && u.Scheme != "https" || u.Hostname() == "" {
		return "", false
	}
	host := strings.ToLower(u.Hostname())
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	port := u.Port()
	if port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 0 || n > 65535 {
			return "", false
		}
		if n == 80 && u.Scheme == "http" || n == 443 && u.Scheme == "https" || n == 0 {
			port = ""
		} else {
			port = strconv.Itoa(n)
		}
	}
	out := u.Scheme + "://" + host
	if port != "" {
		out += ":" + port
	}
	return out, true
}

func hostOf(origin string) string {
	_, rest, _ := strings.Cut(origin, "://")
	if strings.HasPrefix(rest, "[") {
		end := strings.Index(rest, "]")
		return rest[:end+1]
	}
	host, _, _ := strings.Cut(rest, ":")
	return host
}

// session is one accepted socket's state.
type session struct {
	s      *Socket
	c      *conn
	who    caller
	sub    *bus.Sub
	mu     sync.Mutex
	topics map[string]bool
	checks chan bus.Value
	// link is the socket's ended span; tracer starts the spans of its later work.
	link   trace.Link
	tracer trace.Tracer
}

func (s *Socket) serve(ctx context.Context, c *conn, who caller) {
	// Subscribed before the first frame is read, so every "subscribed" is sent with the bus
	// already listening.
	sub, err := s.Hub.Subscribe(bus.Options{Transport: via, Limit: QueueSize, Overflow: bus.DropOldest,
		Keep: func(n *bus.Notice) bool { return n.Event != nil }})
	if err != nil {
		c.close(1011, "")
		return
	}
	defer sub.Close()
	// The socket's span ended when it was accepted. What the socket does later is traced
	// apart, linked to that span, never as its child.
	x := &session{s: s, c: c, who: who, sub: sub, topics: map[string]bool{}, checks: make(chan bus.Value, CheckQueue),
		link:   trace.LinkFromContext(ctx),
		tracer: trace.SpanFromContext(ctx).TracerProvider().Tracer("github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime")}
	ctx = trace.ContextWithSpan(ctx, nil)
	limit := &rate{tokens: RateBurst, at: time.Now()}
	c.take = limit.take
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	parts := []func(context.Context) string{
		func(ctx context.Context) string { return x.read(ctx, limit) },
		x.work, x.pump, x.ping,
		func(ctx context.Context) string { c.keepAlive(ctx.Done()); return "" },
	}
	ended := make(chan string, len(parts))
	for _, part := range parts {
		go func() {
			ended <- guarded(ctx, part)
			cancel()
		}()
	}
	switch <-ended {
	case "signed out":
		c.close(SignedOut, "signed out")
	case "end":
		c.close(1000, "")
	case "rate":
		c.close(1008, "too many frames")
	case "panic":
		c.close(1011, "")
	default:
		c.net.Close()
	}
	for range len(parts) - 1 {
		<-ended
	}
}

// span starts a span for work the socket does after its own span ended: a root, linked to
// the socket's span.
func (x *session) span(ctx context.Context, name string) (context.Context, trace.Span) {
	return x.tracer.Start(ctx, name, trace.WithNewRoot(), trace.WithLinks(x.link))
}

// guarded runs one part of a socket. A part runs after the hijack, outside net/http's
// recovery, so a panic there would end the process: it ends this socket instead ("panic").
func guarded(ctx context.Context, part func(context.Context) string) (out string) {
	defer func() {
		if p := recover(); p != nil {
			slog.ErrorContext(ctx, "a realtime socket panicked; closing it with 1011", "stack", string(debug.Stack()))
			out = "panic"
		}
	}()
	return part(ctx)
}

func (x *session) send(frame string, v bus.Value) error {
	if err := x.c.text(bus.Dumps(v, false, true)); err != nil {
		return err
	}
	x.s.Hub.Sent(via, frame)
	return nil
}

func (x *session) sendError(message string) error {
	return x.send("error", bus.Object("type", "error", "message", message))
}

func (x *session) ping(ctx context.Context) string {
	tick := time.NewTicker(PingEvery)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return ""
		case <-tick.C:
			// A device's token is read again at each keepalive: an expired one ends the socket.
			if !x.deviceLive(ctx) {
				return "signed out"
			}
			if x.send("ping", bus.Object("type", "ping")) != nil {
				return ""
			}
		}
	}
}

// pump forwards the bus to the socket (realtime.py pump): only events under a followed
// topic; a resync when the subscription dropped events or the listener lost some.
func (x *session) pump(ctx context.Context) string {
	dropped := 0
	revocable := x.s.Gate.Mode != auth.Off && x.who.session != ""
	for {
		it, _ := x.sub.Next(ctx, 0)
		if it.End {
			if ctx.Err() != nil {
				return ""
			}
			return "end"
		}
		var e *bus.Event
		if it.Notice != nil {
			e = it.Notice.Event
		}
		if revocable && e != nil && e.Kind == "auth.session_revoked" && x.revokes(e) {
			return "signed out"
		}
		if x.deviceRevoked(e) {
			return "signed out"
		}
		d := x.sub.Dropped()
		resync := d != dropped || it.Gap || e != nil && e.Kind == "bus.resync"
		// Events were lost, a revocation among them maybe: the session is read again.
		if revocable && resync && !x.live(ctx) {
			return "signed out"
		}
		if resync && !x.deviceLive(ctx) {
			return "signed out"
		}
		if d != dropped {
			dropped = d
			x.s.Hub.Resynced(via, "overflow")
			if x.send("resync", bus.Object("type", "resync")) != nil {
				return ""
			}
		}
		if it.Gap || e != nil && e.Kind == "bus.resync" {
			x.s.Hub.Resynced(via, "gap")
			if x.send("resync", bus.Object("type", "resync")) != nil {
				return ""
			}
			continue
		}
		var matched []string
		x.mu.Lock()
		for _, t := range topicsOf(e) {
			if x.topics[t] {
				matched = append(matched, t)
			}
		}
		x.mu.Unlock()
		if len(matched) > 0 {
			if x.c.text(frameOf(e, matched)) != nil {
				return ""
			}
			x.s.Hub.Sent(via, "event")
		}
	}
}

// live says whether the socket's session is still live. A failed read keeps the socket: the
// next resync reads again.
func (x *session) live(ctx context.Context) bool {
	ctx, span := x.span(ctx, "ws session")
	defer span.End()
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	idHash, err := hex.DecodeString(x.who.session)
	if err != nil {
		return false
	}
	var live bool
	err = x.s.Pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM auth_sessions WHERE id_hash = $1
		AND revoked_at IS NULL AND idle_expires_at > $2 AND absolute_expires_at > $2)`,
		idHash, time.Now().UTC()).Scan(&live)
	if err != nil {
		slog.WarnContext(ctx, "could not read a realtime socket's session again", "error", err.Error())
		return true
	}
	return live
}

// revokes is realtime.py revokes: that session, or (no session_id) every one of the
// principal's.
func (x *session) revokes(e *bus.Event) bool {
	if !e.Null("session_id") {
		return e.Text("session_id") == x.who.session
	}
	return e.Text("principal") == x.who.principal
}

// rate is realtime.py RateLimit, a token bucket.
type rate struct {
	tokens float64
	at     time.Time
}

func (r *rate) take() bool {
	now := time.Now()
	r.tokens = min(RateBurst, r.tokens+now.Sub(r.at).Seconds()*RatePerSecond)
	r.at = now
	if r.tokens < 1 {
		return false
	}
	r.tokens--
	return true
}

// read reads the client's messages. limit is the socket's frame rate, which its pings and
// pongs take from too.
func (x *session) read(ctx context.Context, limit *rate) string {
	for {
		m, err := x.c.read()
		if errors.Is(err, errRate) {
			return "rate"
		}
		if err != nil {
			return ""
		}
		if !limit.take() {
			return "rate"
		}
		if err := x.frame(ctx, m); err != nil {
			return ""
		}
	}
}

func (x *session) frame(ctx context.Context, m message) error {
	if m.binary {
		return x.sendError("expected a text frame")
	}
	if utf8.RuneCountInString(m.text) > MaxFrameChars {
		return x.sendError("frame too large")
	}
	frame, err := bus.ParseJSON(m.text)
	if err != nil {
		return x.sendError("not JSON")
	}
	kind := ""
	if t := frame.Get("type"); t != nil && t.Kind == 's' {
		kind = t.Str
	}
	switch {
	case kind == "pong":
		return nil
	case kind == "check":
		return x.submit(frame)
	}
	requested := frame.Get("topics")
	if kind != "subscribe" && kind != "unsubscribe" || requested == nil || requested.Kind != 'a' {
		return x.sendError("expected subscribe or unsubscribe")
	}
	var bad []bus.Value
	calls := 0
	for _, t := range requested.Items {
		// A device follows its own topic only, and a person a device call's only when it
		// owns the device (device.go). Each call topic is one read of the database, so one
		// frame may name at most MaxCallTopics of them.
		if kind == "subscribe" && t.Kind == 's' && callTopic.MatchString(t.Str) {
			if calls++; calls > MaxCallTopics {
				bad = append(bad, t)
				continue
			}
		}
		if !x.topicShaped(t) || kind == "subscribe" && !x.topicAllowed(ctx, t) {
			bad = append(bad, t)
		}
	}
	if len(bad) > 0 {
		return x.sendError("unknown topics: " + bus.ReprList(bad[:min(5, len(bad))]))
	}
	x.mu.Lock()
	if kind == "unsubscribe" {
		for _, t := range requested.Items {
			delete(x.topics, t.Str)
		}
		x.mu.Unlock()
		return nil
	}
	// What fits is followed and the rest refused, so a client that resubscribes everything
	// in one frame after a reconnect never loses all of it to the cap.
	var accepted, refused []string
	for _, t := range unique(requested.Items) {
		if x.topics[t] || len(x.topics) < MaxTopics {
			x.topics[t] = true
			accepted = append(accepted, t)
		} else {
			refused = append(refused, t)
		}
	}
	x.mu.Unlock()
	if len(accepted) > 0 {
		ack := bus.Object("type", "subscribed", "topics", bus.Strings(accepted))
		// Read with the topics already followed: any event after it is sent (#1039).
		if last := x.s.Hub.Live(); last != "" {
			ack = bus.Object("type", "subscribed", "topics", bus.Strings(accepted), "last", last)
		}
		if err := x.send("subscribed", ack); err != nil {
			return err
		}
	}
	if len(refused) > 0 {
		return x.sendError(fmt.Sprintf("at most %d topics; not following %s", MaxTopics,
			bus.ReprList(bus.Strings(refused[:min(5, len(refused))]).Items)))
	}
	return nil
}

// unique is the strings among items, each once, in order (dict.fromkeys).
func unique(items []bus.Value) []string {
	seen := map[string]bool{}
	var out []string
	for _, t := range items {
		if t.Kind == 's' && !seen[t.Str] {
			seen[t.Str] = true
			out = append(out, t.Str)
		}
	}
	return out
}

// submit queues a check for the worker; past CheckQueue waiting it is answered "every topic
// changed" at once (realtime.py _Checks.submit).
func (x *session) submit(frame bus.Value) error {
	select {
	case x.checks <- frame:
		return nil
	default:
	}
	id, requested := frame.Get("id"), frame.Get("topics")
	if isInt(id) && requested != nil && requested.Kind == 'a' {
		var named []string
		for _, t := range unique(requested.Items) {
			if x.topicShaped(bus.Value{Kind: 's', Str: t}) {
				named = append(named, t)
			}
		}
		return x.send("changed", bus.Object("type", "changed", "id", *id, "topics", bus.Strings(named)))
	}
	return x.check(context.Background(), frame)
}

func isInt(v *bus.Value) bool { return v != nil && (v.Kind == 'i' || v.Kind == 'b') }

func (x *session) work(ctx context.Context) string {
	for {
		select {
		case <-ctx.Done():
			return ""
		case frame := <-x.checks:
			if x.check(ctx, frame) != nil {
				return ""
			}
		}
	}
}

// check answers one check frame (realtime.py _check).
func (x *session) check(ctx context.Context, frame bus.Value) error {
	ctx, span := x.span(ctx, "ws check")
	defer span.End()
	id, since, requested := frame.Get("id"), frame.Get("since"), frame.Get("topics")
	valid := isInt(id) && since != nil && since.Kind == 'a' && len(since.Items) >= 1 &&
		len(since.Items) <= CheckMaxSince && requested != nil && requested.Kind == 'a'
	if valid {
		for _, e := range since.Items {
			if n := utf8.RuneCountInString(e.Str); e.Kind != 's' || n == 0 || n > 64 {
				valid = false
			}
		}
		for _, t := range requested.Items {
			valid = valid && x.topicShaped(t)
		}
	}
	if !valid {
		return x.sendError("a check needs an id, since and topics")
	}
	topics := unique(requested.Items)
	var followed []string
	x.mu.Lock()
	for _, t := range topics {
		if x.topics[t] {
			followed = append(followed, t)
		}
	}
	x.mu.Unlock()
	changed := map[string]bool{}
	if len(followed) > 0 {
		got, err := x.changedTopics(ctx, unique(since.Items), followed)
		if err != nil {
			// The log could not be read: the client reads again, as without a cursor.
			slog.WarnContext(ctx, "could not check a realtime cursor", "error", err.Error())
			got = followed
		}
		for _, t := range got {
			changed[t] = true
		}
	}
	var answer []string
	for _, t := range topics {
		if changed[t] || !slicesHas(followed, t) {
			answer = append(answer, t)
		}
	}
	return x.send("changed", bus.Object("type", "changed", "id", *id, "topics", bus.Strings(answer)))
}

func slicesHas(items []string, s string) bool {
	for _, item := range items {
		if item == s {
			return true
		}
	}
	return false
}

// changedTopics is realtime.py changed_topics: which of topics an event logged after the
// earliest of since touched; all of them when that cannot be told.
func (x *session) changedTopics(ctx context.Context, since, topics []string) ([]string, error) {
	places, err := x.s.Hub.Positions(ctx, since)
	if err != nil {
		return nil, err
	}
	var after int64 = -1
	for _, id := range since {
		seq, ok := places[id]
		if !ok {
			return topics, nil
		}
		if after < 0 || seq < after {
			after = seq
		}
	}
	wanted := map[string]bool{}
	for _, t := range topics {
		wanted[t] = true
	}
	changed := map[string]bool{}
	seen := 0
	for {
		page, gap, err := x.s.Hub.Replay(ctx, after, CheckPage)
		if err != nil {
			return nil, err
		}
		if gap {
			return topics, nil
		}
		for _, row := range page {
			e, err := bus.Decode(row.Text)
			if err != nil {
				return nil, err
			}
			for _, t := range topicsOf(e) {
				if wanted[t] {
					changed[t] = true
				}
			}
			after = row.Seq
		}
		seen += len(page)
		if len(page) < CheckPage || len(changed) == len(wanted) {
			break // a short page is the log's end
		}
		if seen >= CheckMaxEvents {
			return topics, nil
		}
	}
	var out []string
	for _, t := range topics {
		if changed[t] {
			out = append(out, t)
		}
	}
	return out, nil
}

// uuidText is realtime.py FLOW_RUN_ID_PATTERN.
const uuidText = `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`

// topicPattern is realtime.py _TOPIC.
var topicPattern = regexp.MustCompile(`^(?:` + strings.Join([]string{
	`job:[0-9a-f]{32}`,
	`model:(builtin:)?[a-z0-9][a-z0-9-]{0,99}`,
	`print:[0-9a-f]{32}`,
	`print:library:[1-9][0-9]{0,17}`,
	`preview-slice:(?:bambuddy|native):[1-9][0-9]{0,17}`,
	`workflow-run:` + uuidText,
	`printer:` + uuidText,
	`order:` + uuidText,
	`task:` + uuidText,
	`tasks:[^\x00-\x1f\x7f-\x9f]{1,100}`,
	`activity:(?:render|operation|print_run|agent_operation):[A-Za-z0-9_-]{1,64}`,
	`activity`, `analyzers`, `flows`, `fonts`, `libraries`, `models`, `orders`, `outputs`,
	`plugin-packages`, `printers`, `settings`, `tasks`, `workflow-runs`,
}, "|") + `)$`)

// ValidTopic is realtime.py valid_topic.
func ValidTopic(v bus.Value) bool {
	if v.Kind != 's' || !topicPattern.MatchString(v.Str) {
		return false
	}
	return !strings.HasPrefix(v.Str, "model:") || utf8.RuneCountInString(v.Str)-6 <= len("builtin:")+100
}
