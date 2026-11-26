// Package changes is area changes: the service change streams GET
// /api/v1/internal/crud/<caller>/changes, server-sent events of what the API's LISTEN
// connection hears, as backend/scadbuddy/agent_changes/hub.py, api/crud_agent_changes.py,
// crud_fleet.py, crud_camera.py and crud_camera_relay.py serve them (#7768).
//
// Frames: "change" ({"c": channel, "p": payload}, with an id when it is a logged event),
// "ready" ({"resync", "reconnect"}, with the stream's place as its id) once after a
// reconnect's replay, "reconnected" ({"resync"}) after a gap here, and a keepalive comment
// every 15 s. A Last-Event-ID (a logged event's id, or "0") is replayed from the events log
// before ready, at most 1000 events; an id the log cannot place, a pruned gap or a longer
// replay is a resync.
package changes

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/front"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

const (
	// Start is hub.py START: the place before the first logged event.
	Start = "0"
	// QueueMax is hub.py QUEUE_MAX.
	QueueMax = 10_000
	// ReplayMax is hub.py REPLAY_MAX.
	ReplayMax = 1000
	// LastEventIDMax is crud_agent_changes.py LAST_EVENT_ID_MAX.
	LastEventIDMax = 64
	// FleetStreamsMax and CameraStreamsMax are crud_fleet.py FLEET_STREAMS_MAX and
	// crud_camera.py CAMERA_STREAMS_MAX.
	FleetStreamsMax  = 8
	CameraStreamsMax = 8
	// FleetKindPrefix is crud_agent_changes.py FLEET_KIND_PREFIX.
	FleetKindPrefix = "fleet."
	via             = "changes"
)

// Keepalive is hub.py KEEPALIVE_SECONDS.
var Keepalive = 15 * time.Second

// eventID is hub.py EVENT_ID: an id outside it names no place.
var eventID = regexp.MustCompile(`^[0-9A-Za-z-]{1,64}$`)

// cameraKinds is crud_camera.py CAMERA_KINDS.
var cameraKinds = map[string]bool{"printer.changed": true, "camera.reconnect": true}

// kindOf is the payload's kind as json.loads(payload).get("kind") reads it; ok is false
// when that raises (not JSON, or not an object).
func kindOf(payload string) (kind any, ok bool) {
	v, err := bus.ParseJSON(payload)
	if err != nil || v.Kind != 'o' {
		return nil, false
	}
	k := v.Get("kind")
	if k == nil {
		return nil, true
	}
	if k.Kind == 's' {
		return k.Str, true
	}
	return k, true
}

// KeepAgent is crud_agent_changes.py keep_agent: every change but the event log's fleet.
// kinds, which name the fleet's session ids.
func KeepAgent(n *bus.Notice) bool {
	if n.Channel != bus.Channel {
		return true
	}
	kind, ok := kindOf(n.Payload)
	s, isText := kind.(string)
	return !ok || !(isText && strings.HasPrefix(s, FleetKindPrefix))
}

// KeepFleet is crud_fleet.py keep_fleet: only the event log's fleet. kinds.
func KeepFleet(n *bus.Notice) bool {
	if n.Channel != bus.Channel {
		return false
	}
	kind, ok := kindOf(n.Payload)
	s, isText := kind.(string)
	return ok && isText && strings.HasPrefix(s, FleetKindPrefix)
}

// KeepCamera is crud_camera.py keep_camera: only the event log's camera kinds.
func KeepCamera(n *bus.Notice) bool {
	if n.Channel != bus.Channel {
		return false
	}
	kind, ok := kindOf(n.Payload)
	s, isText := kind.(string)
	return ok && isText && cameraKinds[s]
}

// stream is one caller's stream: its filter and its budget's group.
type stream struct {
	keep     func(*bus.Notice) bool
	group    string
	groupMax int
}

// Streams are the callers Python serves a change stream to.
var Streams = map[string]stream{
	"agent":        {keep: KeepAgent},
	"fleet":        {keep: KeepFleet, group: "fleet", groupMax: FleetStreamsMax},
	"camera":       {keep: KeepCamera, group: "camera", groupMax: CameraStreamsMax},
	"camera-relay": {keep: KeepCamera, group: "camera", groupMax: CameraStreamsMax},
}

// Routes are every caller's GET /changes, over hub; trusted names the proxies whose
// X-Forwarded-For gives a stream's peer address.
func Routes(hub *bus.Hub, trusted front.Networks) map[string][]crud.Route {
	out := map[string][]crud.Route{}
	for caller, s := range Streams {
		out[caller] = []crud.Route{{Method: "GET", Path: "/changes", Handle: func(r *http.Request, _ crud.Params) (any, error) {
			return open(r, hub, trusted, s)
		}}}
	}
	return out
}

// key is crud_agent_changes.py stream_key: the credential and the peer's address. Each agent
// process connects from its own pod's address, so the pair counts one process's streams.
func key(r *http.Request, trusted front.Networks) string {
	credential := "unknown"
	if s := auth.ServiceOf(r.Context()); s != nil {
		credential = s.Caller
		if s.ID != "" {
			credential = s.ID
		}
	}
	return credential + "@" + trusted.Client(r)
}

func open(r *http.Request, hub *bus.Hub, trusted front.Networks, s stream) (any, error) {
	var lastID *string
	if values, ok := r.Header["Last-Event-Id"]; ok && len(values) > 0 {
		v := values[0]
		if len(v) > LastEventIDMax {
			v = "" // no place: a resync, as any other malformed id
		}
		lastID = &v
	}
	// Read before the stream subscribes, so every live event comes after its place.
	start := ""
	if lastID == nil {
		newest, err := hub.Newest(r.Context())
		if err != nil {
			return nil, err
		}
		start = or(newest, Start)
	}
	budget := &bus.Budget{Key: key(r, trusted), Group: s.group, GroupMax: s.groupMax}
	sub, err := hub.Subscribe(bus.Options{Transport: via, Keep: s.keep, Limit: QueueMax,
		Overflow: bus.GapOnOverflow, Budget: budget})
	var full *bus.ErrFull
	if errors.As(err, &full) {
		return nil, &httpx.Problem{Status: http.StatusTooManyRequests, Detail: full.Reason}
	}
	if err != nil {
		return nil, err
	}
	return crud.Stream(func(w http.ResponseWriter) {
		defer sub.Close()
		serve(r.Context(), w, hub, sub, lastID, start)
	}), nil
}

func or(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

// place is the stream's place in the log: the last logged id sent, and the ids its replay
// sent (hub.py _Place).
type place struct {
	last     string
	replayed map[string]bool
}

// serve writes the stream until the client leaves or the hub closes (hub.py frames).
func serve(ctx context.Context, w http.ResponseWriter, hub *bus.Hub, sub *bus.Sub, lastID *string, start string) {
	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	_ = flush.Flush()
	// A stream may stay open for hours: its span ends as it starts.
	telemetry.Streaming(ctx)
	write := func(frame, text string) bool {
		if _, err := w.Write([]byte(text)); err != nil {
			return false
		}
		hub.Sent(via, frame)
		return flush.Flush() == nil
	}
	reconnect := lastID != nil
	var at place
	resync := false
	if reconnect {
		var frames []string
		frames, at, resync = replay(ctx, hub, *lastID, sub)
		for _, f := range frames {
			if !write("change", f) {
				return
			}
		}
	} else {
		at = place{last: start, replayed: map[string]bool{}}
	}
	if resync {
		hub.Resynced(via, "place")
	}
	if !write("ready", bus.SSE("ready", bus.Object("resync", resync, "reconnect", reconnect), at.last)) {
		return
	}
	for {
		it, ok := sub.Next(ctx, Keepalive)
		switch {
		case !ok:
			if !write("keepalive", bus.Keepalive) {
				return
			}
		case it.End:
			return
		case it.Gap:
			frames, gapAt, resync := replay(ctx, hub, at.last, sub)
			for _, f := range frames {
				if !write("change", f) {
					return
				}
			}
			at = gapAt
			if resync {
				hub.Resynced(via, "gap")
			}
			if !write("reconnected", bus.SSE("reconnected", bus.Object("resync", resync), at.last)) {
				return
			}
		default:
			n := it.Notice
			id := n.LoggedID()
			if id != "" {
				if at.replayed[id] {
					continue
				}
				at.last = id
			}
			if !write("change", bus.ChangeFrame(n.Channel, n.Payload, id)) {
				return
			}
		}
	}
}

func placeable(id string) bool { return id == Start || eventID.MatchString(id) }

// replay is the frames of the log after lastID, the place after them, and whether the client
// must resync instead: its place unknown, pruned past, or too far behind (hub.py _replay).
// A filtered stream's place moves past every event it scans, kept or not.
func replay(ctx context.Context, hub *bus.Hub, lastID string, sub *bus.Sub) ([]string, place, bool) {
	resync := func() ([]string, place, bool) {
		newest, err := hub.Newest(ctx)
		if err != nil && ctx.Err() == nil {
			slog.WarnContext(ctx, "could not read the event log's newest id", "error", err.Error())
		}
		return nil, place{last: or(newest, Start), replayed: map[string]bool{}}, true
	}
	if !placeable(lastID) {
		return resync()
	}
	var after int64
	if lastID != Start {
		seq, ok, err := hub.Position(ctx, lastID)
		if err != nil || !ok {
			return resync()
		}
		after = seq
	}
	at := place{last: lastID, replayed: map[string]bool{}}
	var frames []string
	for {
		page, gap, err := hub.Replay(ctx, after, bus.ReplayMax)
		if err != nil {
			return resync()
		}
		if gap || len(at.replayed)+len(page) > ReplayMax {
			return resync()
		}
		if len(page) == 0 {
			return frames, at, false
		}
		for _, row := range page {
			e, err := bus.Decode(row.Text)
			if err != nil {
				// Replaying past it would fail the same way at every reconnect: re-read instead.
				slog.WarnContext(ctx, "the event log holds an event this build cannot decode; resyncing")
				return resync()
			}
			at.last = e.ID
			at.replayed[e.ID] = true
			payload := e.Encoded()
			if !sub.Keep(&bus.Notice{Channel: bus.Channel, Payload: payload, Event: e}) {
				continue
			}
			frames = append(frames, bus.ChangeFrame(bus.Channel, payload, e.ID))
		}
		after = page[len(page)-1].Seq
	}
}
