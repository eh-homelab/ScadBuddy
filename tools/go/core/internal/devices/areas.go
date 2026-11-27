// Package devices is area devices (#8244, design docs/superpowers/specs/2026-11-27-devices-design.md):
// the phone app, paired to a person as a device. A person makes a one-time pairing code; the
// app claims it and gets a device token that expires after an idle hour. Pages, flows and the
// assistant call the device's methods (nfc.read, nfc.write, code.scan, camera.capture). A call
// goes to the device as the bus event device.call_requested on the topic device:<id>, which the
// device's realtime socket follows on whichever pod holds it. The device posts its events and
// its result over HTTP; core publishes each event on the bus with the topics device:<id> and
// device:call:<call id>. Bytes go to the blob store (blobstore.Put), never in an event.
//
// Two kinds of route:
//   - Person routes (class api) follow SCADBUDDY_AUTH_MODE, as every other public route does.
//   - Device routes (class public to the gate) check the device token in every mode, so a
//     stolen token dies after its idle hour even on a host that runs observe.
package devices

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// Area is the area's name in SCADBUDDY_CORE_AREAS.
	Area = "devices"
	// TokenPrefix starts every device token.
	TokenPrefix = "sbdev_"

	// DefaultIdle is how long a token lives unused; DefaultHardCap ends every pairing.
	DefaultIdle    = time.Hour
	DefaultHardCap = 30 * 24 * time.Hour
	// CodeTTL is how long a pairing code may be claimed.
	CodeTTL = 10 * time.Minute
	// OnlineWindow is how old a device's last request may be while it is online: two
	// heartbeats of 30 s and some slack.
	OnlineWindow = 75 * time.Second
	// DefaultTimeout and MaxTimeout bound a call's deadline, in seconds.
	DefaultTimeout = 60
	MaxTimeout     = 120
	// Retention is how long the event log keeps a device event.
	Retention = 7 * 24 * 60 * 60
	// ApprovalTTL is how long an assistant's or a flow's nfc.write waits for a person.
	ApprovalTTL = 10 * time.Minute

	bodyMax = 4 << 10
	// argsMax is the most bytes a call's args or result hold (the table's CHECK).
	argsMax = 4096
)

// The four methods. ScadBuddy is not a device platform: there are no others.
var Methods = []string{"nfc.read", "nfc.write", "code.scan", "camera.capture"}

// ErrUnauthorized is a missing, bad, revoked or expired device token.
var ErrUnauthorized = errors.New("devices: no live device token")

// Blobs is the blob store as the area uses it: blobstore.Blobs.
type Blobs interface {
	Put(ctx context.Context, kind, mediaType string, body []byte) (blobstore.Committed, error)
	OpenKey(ctx context.Context, key string) (io.ReadCloser, blobstore.Ref, error)
}

// Store is the area's state: its pool and its settings.
type Store struct {
	Pool  *pgxpool.Pool
	Keys  *seal.Keys
	Blobs Blobs
	// Now is the clock; tests inject one.
	Now func() time.Time
	// Idle and HardCap are SCADBUDDY_DEVICE_IDLE and SCADBUDDY_DEVICE_HARD_CAP.
	Idle, HardCap time.Duration
	// Poll is how often a call's ?wait=true reads the call again (default 250 ms).
	Poll time.Duration
	// After schedules a call's deadline check; time.AfterFunc by default.
	After func(d time.Duration, f func())
	// Delegates are the service callers that may call a device for a person they name
	// (SCADBUDDY_DEVICE_DELEGATES). A credential whose scopes hold DelegateScope may too.
	Delegates []string
}

// DelegateScope is the service scope that grants calling a device on a person's behalf.
const DelegateScope = "devices.call"

// DefaultDelegates are the callers that hold the grant when SCADBUDDY_DEVICE_DELEGATES is
// not set: the agent (the assistant) and the projects worker (flows).
var DefaultDelegates = []string{"agent", "projects"}

// New is the area over pool, with its settings read from lookupEnv (CheckEnv has refused a
// bad one at the start).
func New(pool *pgxpool.Pool, keys *seal.Keys, blobs Blobs, lookupEnv func(string) (string, bool)) *Store {
	s := &Store{Pool: pool, Keys: keys, Blobs: blobs, Now: time.Now, Idle: DefaultIdle, HardCap: DefaultHardCap,
		Delegates: DefaultDelegates}
	if lookupEnv != nil {
		s.Idle, s.HardCap, _ = settings(lookupEnv)
		s.Delegates = delegates(lookupEnv)
	}
	return s
}

// delegates is SCADBUDDY_DEVICE_DELEGATES, comma-separated; set but empty, no caller holds
// the grant by its name.
func delegates(lookupEnv func(string) (string, bool)) []string {
	raw, ok := lookupEnv("SCADBUDDY_DEVICE_DELEGATES")
	if !ok {
		return DefaultDelegates
	}
	out := []string{}
	for item := range strings.SplitSeq(raw, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

// CheckEnv refuses a setting the area cannot use, so a bad value stops the start.
func CheckEnv(lookupEnv func(string) (string, bool)) error {
	_, _, err := settings(lookupEnv)
	return err
}

func settings(lookupEnv func(string) (string, bool)) (idle, hardCap time.Duration, err error) {
	idle, hardCap = DefaultIdle, DefaultHardCap
	read := func(name string, into *time.Duration) {
		raw, ok := lookupEnv(name)
		if !ok || strings.TrimSpace(raw) == "" || err != nil {
			return
		}
		d, e := time.ParseDuration(strings.TrimSpace(raw))
		if e != nil || d <= 0 {
			err = fmt.Errorf("%s must be a positive Go duration (1h, 720h), not %q", name, raw)
			return
		}
		*into = d
	}
	read("SCADBUDDY_DEVICE_IDLE", &idle)
	read("SCADBUDDY_DEVICE_HARD_CAP", &hardCap)
	if err == nil && idle > hardCap {
		err = errors.New("SCADBUDDY_DEVICE_IDLE is longer than SCADBUDDY_DEVICE_HARD_CAP")
	}
	return idle, hardCap, err
}

func (s *Store) now() time.Time { return s.Now().UTC() }

func (s *Store) after(d time.Duration, f func()) {
	if s.After != nil {
		s.After(d, f)
		return
	}
	time.AfterFunc(d, f)
}

// lookupEnv is the process's environment, which the area and the blob store read.
var lookupEnv = os.LookupEnv

// Routes are the area's public routes (main.go publicPackages).
func Routes(pool *pgxpool.Pool, keys *seal.Keys) map[string][]auth.Public {
	s := New(pool, keys, blobstore.New(pool, keys, lookupEnv, nil), lookupEnv)
	return map[string][]auth.Public{Area: s.Public()}
}

// Public are s's routes.
func (s *Store) Public() []auth.Public {
	person := func(method, path string, handle func(*http.Request) (any, error), maxBody int64, scriptless bool) auth.Public {
		return auth.Public{
			Pattern: method + " " + path,
			Route:   auth.Route{Class: "api", Template: path, Scriptless: scriptless},
			Handler: crud.Serve(handle, maxBody),
		}
	}
	// The gate never refuses a "public" route; the handler checks the device token itself,
	// in every mode.
	device := func(method, path string, handle func(*http.Request, Device) (any, error), maxBody int64) auth.Public {
		return auth.Public{
			Pattern: method + " " + path,
			Route:   auth.Route{Class: "public", Template: path},
			Handler: crud.Serve(func(r *http.Request) (any, error) {
				d, err := s.Authenticate(r.Context(), r.Header.Get("Authorization"))
				if err != nil {
					return nil, unauthorized(err)
				}
				return handle(r, d)
			}, maxBody),
		}
	}
	return []auth.Public{
		person("POST", "/api/v1/devices/pairings", s.pairing, bodyMax, false),
		person("GET", "/api/v1/devices", s.list, 0, false),
		person("DELETE", "/api/v1/devices/{id}", s.revoke, 0, false),
		person("POST", "/api/v1/devices/{id}/calls", s.call, bodyMax, false),
		person("GET", "/api/v1/devices/calls/{id}", s.getCall, 0, false),
		person("POST", "/api/v1/devices/calls/{id}/confirm", s.confirm, 0, false),
		person("GET", "/api/v1/devices/reads/{id}", s.getRead, 0, false),
		person("GET", "/api/v1/devices/reads/{id}/blob", s.readBlob, 0, true),
		{
			Pattern: "POST /api/v1/devices/pairings/{code}/claim",
			Route:   auth.Route{Class: "public", Template: "/api/v1/devices/pairings/{code}/claim"},
			Handler: crud.Serve(s.claim, bodyMax),
		},
		device("POST", "/api/v1/devices/heartbeat", s.heartbeat, 0),
		device("POST", "/api/v1/devices/calls/{id}/events", s.events, 16<<10),
		device("POST", "/api/v1/devices/calls/{id}/result", s.result, bodyMax),
		device("POST", "/api/v1/devices/uploads", s.upload, blobstore.Kinds["scan_image"]),
	}
}

// principal is who a person route acts for: the signed-in person or the token's, else
// "anonymous" (auth off, or observe serving a signed-out request).
func principal(ctx context.Context) string {
	if c := auth.CallerOf(ctx); c.Principal != "" {
		return c.Principal
	}
	return "anonymous"
}

func unauthorized(err error) error {
	if !errors.Is(err, ErrUnauthorized) {
		return err
	}
	return &httpx.Problem{Status: http.StatusUnauthorized, Detail: "This device is not paired, or its pairing ended.",
		Headers:    map[string]string{"WWW-Authenticate": "Bearer"},
		Extensions: []httpx.Field{{Key: "reason", Value: "device_unauthorized"}}}
}

func notFound(what string) error {
	return httpx.WithReason(http.StatusNotFound, "That "+what+" is not there.", what+"_not_found")
}

func logFailed(ctx context.Context, what string, err error) {
	slog.WarnContext(ctx, "devices: "+what, "error", err.Error())
}
