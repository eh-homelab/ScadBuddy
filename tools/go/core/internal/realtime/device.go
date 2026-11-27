package realtime

import (
	"context"
	"log/slog"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// The device path (#8244): a paired device holds the socket at DevicePath with its device
// token (Authorization: Bearer sbdev_…) to hear its calls. Only devices use that path, and
// only it may be public away from home: Path stays behind the SSO host's login. On
// DevicePath the token is checked in every auth mode, never by the gate, so a handshake with
// no token, with a person's credential, or with a revoked or expired token is refused even
// where the host runs observe or off. Path refuses a device token. A device follows only its
// own topic, device:<id>. Its socket closes with
// SignedOut on device.revoked for it, and when its token is no longer live (read at each
// keepalive and at each resync).
//
// A person's page follows device:call:<id> for a call of a device it owns, to see the
// call's events. Device events are opaque to Python's model (no typed kind), so the topics
// and the frame's data are the event's own fields.

// DeviceTokens is the devices area as the socket uses it (devices.Store).
type DeviceTokens interface {
	// DeviceOf is the device the Authorization header's token names; it moves the token's
	// expiry, as every device request does.
	DeviceOf(ctx context.Context, authorization string) (string, error)
	// Live says whether the token is live now, without moving its expiry. An error is a
	// failed read, which says nothing about the token.
	Live(ctx context.Context, authorization string) (string, bool, error)
	// CallVisible says whether principal may follow the call's events.
	CallVisible(ctx context.Context, call, principal string) bool
}

// DevicePath is the socket's route for a paired device.
const DevicePath = "/api/v1/devices/ws"

const (
	devicePrefix = "sbdev_"
	// DeviceTopicPrefix and CallTopicPrefix start the devices area's topics.
	DeviceTopicPrefix = "device:"
	CallTopicPrefix   = "device:call:"
)

var callTopic = regexp.MustCompile(`^device:call:` + uuidText + `$`)

// MaxCallTopics is the most distinct call topics one subscribe frame may name. Each distinct
// topic is one read of who may follow the call, once per frame however often it is named.
const MaxCallTopics = 8

// deviceBearer says whether the handshake carries a device token.
func deviceBearer(r *http.Request) bool {
	scheme, token, _ := strings.Cut(r.Header.Get("Authorization"), " ")
	return strings.EqualFold(scheme, "bearer") && strings.HasPrefix(strings.TrimSpace(token), devicePrefix)
}

// openDevice serves a handshake on DevicePath: a live device token first, in every mode.
func (s *Socket) openDevice(w http.ResponseWriter, r *http.Request) {
	telemetry.SetRoute(r.Context(), DevicePath)
	if s.Devices == nil {
		refuse(w)
		return
	}
	if !deviceBearer(r) {
		unauthorized(w)
		return
	}
	authorization := r.Header.Get("Authorization")
	id, err := s.Devices.DeviceOf(r.Context(), authorization)
	if err != nil {
		unauthorized(w)
		return
	}
	s.accepted(w, r, caller{device: id, authorization: authorization})
}

// unauthorized refuses a handshake with 401 and no body.
func unauthorized(w http.ResponseWriter) {
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(http.StatusUnauthorized)
}

// deviceEvent says whether e is a devices area event: opaque, of namespace device.
func deviceEvent(e *bus.Event) bool { return e.Opaque && strings.HasPrefix(e.Kind, "device.") }

// topicsOf is every topic e is news for on this socket: a typed event's (Event.Topics), or a
// device event's own.
func topicsOf(e *bus.Event) []string {
	if deviceEvent(e) {
		return e.OwnTopics()
	}
	return e.Topics()
}

// frameOf is e's socket frame for the matched topics.
func frameOf(e *bus.Event, matched []string) string {
	if deviceEvent(e) {
		return e.OpaqueFrame(matched)
	}
	return e.Frame(matched)
}

// deviceRevoked says whether e revokes this socket's device.
func (x *session) deviceRevoked(e *bus.Event) bool {
	return x.who.device != "" && e != nil && e.Kind == "device.revoked" &&
		slices.Contains(e.OwnTopics(), DeviceTopicPrefix+x.who.device)
}

// deviceLive says whether this socket's device token is still live; a person's socket is.
func (x *session) deviceLive(ctx context.Context) bool {
	if x.who.device == "" {
		return true
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	id, ok, err := x.s.Devices.Live(ctx, x.who.authorization)
	if err != nil {
		// A failed read is no proof of a revoked token: the socket stays, and the next
		// keepalive or resync reads again.
		slog.WarnContext(ctx, "could not read a device token", "error", err.Error())
		return true
	}
	return ok && id == x.who.device
}

// topicAllowed says whether this socket may follow t: a device its own topic only; a person
// the realtime topics, and the topic of a call whose device it owns or that it made.
func (x *session) topicAllowed(ctx context.Context, t bus.Value) bool {
	if x.who.device != "" {
		return t.Kind == 's' && t.Str == DeviceTopicPrefix+x.who.device
	}
	if t.Kind == 's' && callTopic.MatchString(t.Str) {
		if x.s.Devices == nil {
			return false
		}
		ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		return x.s.Devices.CallVisible(ctx, strings.TrimPrefix(t.Str, CallTopicPrefix), x.who.person())
	}
	return ValidTopic(t)
}

// topicShaped says whether t is a topic this socket may name in a check: a check reads no
// event's fields, only whether a topic changed, and it answers "changed" for every topic the
// socket does not follow. Only topicAllowed lets a socket follow a call's topic, so a check of
// another person's call tells nothing (devices TestCheckOfAnotherCallTellsNothing).
func (x *session) topicShaped(t bus.Value) bool {
	if x.who.device != "" {
		return t.Kind == 's' && t.Str == DeviceTopicPrefix+x.who.device
	}
	return ValidTopic(t) || t.Kind == 's' && callTopic.MatchString(t.Str)
}
